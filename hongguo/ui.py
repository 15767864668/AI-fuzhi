"""Gradio 本地客户端界面。

界面只负责读取控件、展示进度；计费请求、素材复制、项目持久化和
FFmpeg 调用均由 service 层处理。所有改变项目的事件共用串行队列。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from .key_store import LocalKeyStore
from typing import Any

import gradio as gr

from .media import EXPORT_PRESETS, check_dependencies
from .models import DEFAULT_SETTINGS
from .script import parse_script_text, rows_from_segments
from .service import StudioService
from .subtitles import SUBTITLE_STYLES

HEADERS = ["ID", "台词", "开始秒", "结束秒", "角色", "画面提示词", "源视频裁剪起点秒"]
BACKENDS = [("MiniMax官方H3 API", "official"), ("自建GPU H3服务", "gpu")]
QUEUE = {"concurrency_id": "studio", "concurrency_limit": 1}


def _file_path(value: Any) -> str:
    """兼容上传控件与旧项目内的路径值。"""
    if not value:
        return ""
    if isinstance(value, dict):
        return str(value.get("path", value.get("name", "")))
    return str(getattr(value, "name", value))


def _existing(path: str | None) -> str | None:
    return str(path) if path and Path(path).is_file() else None


def _gallery(project: Any) -> list[tuple[str, str]]:
    return [(path, name) for name, path in project.characters.items() if _existing(path)]


def _id_update(project: Any, value: str | None = None) -> Any:
    ids = [seg.id for seg in project.segments]
    return gr.update(choices=ids, value=value if value in ids else (ids[0] if ids else None))


def _first_clip(project: Any) -> str | None:
    return next((_existing(seg.clip_path) for seg in project.segments if _existing(seg.clip_path)), None)


def build_ui(workspace: str | Path | None = None) -> gr.Blocks:
    """创建 UI，不启动 Web 服务，便于脚本测试和本地启动器复用。"""
    data_root = Path(workspace or os.environ.get("HONGGUO_DATA_DIR", "data")).resolve()
    service = StudioService(data_root)
    key_store = LocalKeyStore(data_root / "api_keys.json")
    setting_widgets: dict[str, gr.Component] = {}
    config_widgets: dict[str, gr.Component] = {}

    def setting(key: str, widget: gr.Component) -> gr.Component:
        setting_widgets[key] = widget
        return widget

    def config(key: str, widget: gr.Component) -> gr.Component:
        config_widgets[key] = widget
        return widget

    dependency_problems = check_dependencies()
    defaults = copy.deepcopy(DEFAULT_SETTINGS)
    d = lambda key, fallback: defaults.get(key, fallback)
    css = """
    .gradio-container {max-width: 1360px !important; margin: auto;}
    #studio-title {margin-bottom: 4px;}
    #status {border-left: 3px solid #f05b4f; padding-left: 12px;}
    footer {display: none !important;}
    """
    with gr.Blocks(title="AI 短剧成片工具", css=css, theme=gr.themes.Soft(primary_hue="red")) as demo:
        # 页面加载事件为每个浏览器会话创建独立项目，并使用同一个串行队列。
        project_state = gr.State(value=None)
        gr.Markdown("# AI 短剧成片工具", elem_id="studio-title")
        gr.Markdown("剧本 → H3 原生人声与对口型 → 字幕 → 混音剪辑 → 成片")
        with gr.Row():
            title = gr.Textbox(label="剧本名称", value="未命名短剧", scale=4)
            new_button = gr.Button("新建项目", scale=1)
        status = gr.Markdown("就绪。先录入剧本、设置角色图，再生成片段。", elem_id="status")
        gr.Markdown("**本机依赖检查：**\n\n" + ("\n\n".join("⚠ " + item for item in dependency_problems) if dependency_problems else "FFmpeg / ffprobe、字幕与混音滤镜及编码器检查通过。"))

        with gr.Tab("1 · 剧本与素材"):
            gr.Markdown("时间为**正文时间轴**，不包含片头；每行对应一个 H3 片段。按时间递增排列且不得重叠，行间空隙补黑场/静音；源视频裁剪起点从原片第几秒开始取用。修改后点击应用表格。")
            script_table = gr.Dataframe(
                label="分镜与台词（可直接编辑、增加和删除行）", headers=HEADERS,
                datatype=["str", "str", "number", "number", "str", "str", "number"],
                type="array", value=[], row_count=(1, "dynamic"), col_count=(7, "fixed"),
                interactive=True, wrap=True,
            )
            apply_table = gr.Button("应用表格 / 更新片段列表")
            with gr.Accordion("批量录入 / 导入 TXT", open=True):
                bulk_text = gr.Textbox(label="批量剧本", lines=5, placeholder="每行一句台词；带时间格式：[0-5] 女主|你终于来了。|夜景，近景")
                with gr.Row():
                    default_duration = gr.Number(label="无时间轴时，每句默认秒数", value=5, minimum=0.1, precision=2)
                    append_mode = gr.Checkbox(label="追加到现有剧本末尾", value=False)
                    apply_bulk = gr.Button("应用批量剧本")
                txt_upload = gr.File(label="导入 TXT（UTF-8 / GB18030；上传后自动导入）", file_types=[".txt"], type="filepath")
            with gr.Row():
                with gr.Column():
                    gr.Markdown("### 角色参考图")
                    character_name = gr.Textbox(label="角色名称", placeholder="例如：林晚（与表格角色列一致）")
                    character_image = gr.Image(label="上传角色参考图", type="filepath", sources=["upload"])
                    with gr.Row():
                        add_character = gr.Button("保存 / 替换角色图")
                        remove_character = gr.Button("删除该角色参考图")
                    characters_gallery = gr.Gallery(label="已保存角色", columns=3, height=240, object_fit="contain", interactive=False)
                with gr.Column():
                    gr.Markdown("### 配乐与片头片尾")
                    bgm = gr.Audio(label="本地 BGM（上传即保存；清空即移除）", type="filepath", sources=["upload"])
                    with gr.Row():
                        intro = gr.Video(label="片头（可选，上传即保存）", sources=["upload"], format=None)
                        outro = gr.Video(label="片尾（可选，上传即保存）", sources=["upload"], format=None)
                    gr.Markdown("H3 片段自带人声，无需额外 TTS 或对口型模型。片头片尾会统一规格后拼接。")

        with gr.Tab("2 · 生成与导出"):
            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("### 视频生成后端")
                    backend = config("backend", gr.Dropdown(label="后端切换", choices=BACKENDS, value="official"))
                    official_key = gr.Textbox(label="MiniMax API Key", type="password", placeholder="可留空，读取 MINIMAX_API_KEY", value="")
                    remember_key = gr.Checkbox(label="记住上次使用的 API Key（重新打开网页自动载入）", value=True)
                    with gr.Accordion("本地 API Key 管理（仅本机）", open=True):
                        gr.Markdown("密钥保存在运行程序的电脑的数据目录中，不写入项目包或 GitHub。**本地文件未加密**，请勿在多人共用或公开部署的服务上使用。")
                        saved_key = gr.Dropdown(label="选择已保存的 MiniMax Key", choices=[], interactive=True)
                        key_name = gr.Textbox(label="密钥名称", placeholder="例如：我的主账号")
                        with gr.Row():
                            save_key_button = gr.Button("保存当前 Key")
                            select_key_button = gr.Button("使用所选 Key")
                        with gr.Row():
                            delete_key_button = gr.Button("删除所选已保存 Key")
                            clear_key_button = gr.Button("清空输入框")
                        key_notice = gr.Markdown("")
                    gpu_key = gr.Textbox(label="GPU 服务 API Key（仅当前会话）", type="password", placeholder="可留空，读取 H3_GPU_API_KEY", value="", visible=False)
                    with gr.Row():
                        config("model", gr.Textbox(label="模型", value="MiniMax-H3"))
                        config("resolution", gr.Dropdown(label="生成分辨率", choices=["480P", "768P", "2K"], value="768P", allow_custom_value=True))
                        config("ratio", gr.Dropdown(label="生成画面比例", choices=["9:16", "16:9", "1:1", "3:4", "4:3", "21:9", "adaptive"], value="9:16"))
                    with gr.Accordion("接口与网络参数", open=False):
                        base_url = config("base_url", gr.Textbox(label="服务地址", value="https://api.minimax.cn"))
                        create_path = config("create_path", gr.Textbox(label="创建任务路径", value="/v2/video_generation"))
                        query_path = config("query_path", gr.Textbox(label="查询任务路径（保留 {task_id}）", value="/v2/query/video_generation/{task_id}"))
                        with gr.Row():
                            config("timeout", gr.Number(label="单次请求超时（秒）", value=60, minimum=1, precision=0))
                            config("poll_interval", gr.Number(label="轮询间隔（秒）", value=5, minimum=0.1))
                            config("max_wait", gr.Number(label="任务总等待上限（秒）", value=1800, minimum=1, precision=0))
                        config("extra_json", gr.Textbox(label="额外请求字段（JSON 对象）", value="{}", lines=3))
                    gr.Markdown("官方 H3 每次生成 4–15 秒（含源视频裁剪起点）；短台词生成后裁剪。创建请求不自动重发；生成按钮可继续查询已保存任务。单段重新生成会创建新的计费任务。")
                with gr.Column(scale=1):
                    gr.Markdown("### 导出规格")
                    preset = gr.Dropdown(label="红果短剧建议模板", choices=list(EXPORT_PRESETS), value=next(iter(EXPORT_PRESETS), None))
                    gr.Markdown("模板为常用竖屏建议参数。实际上传要求以红果创作者后台为准；程序会检查媒体规格并提示差异。")
                    with gr.Row():
                        setting("width", gr.Number(label="宽度", value=d("width", 1080), minimum=2, precision=0))
                        setting("height", gr.Number(label="高度", value=d("height", 1920), minimum=2, precision=0))
                        setting("fps", gr.Number(label="帧率", value=d("fps", 30), minimum=1, precision=0))
                    with gr.Row():
                        setting("video_bitrate", gr.Textbox(label="视频码率", value=d("video_bitrate", "6000k")))
                        setting("audio_bitrate", gr.Textbox(label="音频码率", value=d("audio_bitrate", "192k")))
                        setting("sample_rate", gr.Dropdown(label="音频采样率", choices=[44100, 48000], value=d("sample_rate", 48000)))
                    setting("fit", gr.Dropdown(label="画面适配", choices=[("填满画面（居中裁剪）", "crop"), ("完整保留（补黑边）", "pad")], value=d("fit", "crop")))
            with gr.Row():
                with gr.Column():
                    gr.Markdown("### 字幕与水印")
                    setting("subtitle_enabled", gr.Checkbox(label="烧录 ASS 字幕", value=d("subtitle_enabled", True)))
                    setting("subtitle_style", gr.Dropdown(label="字幕样式", choices=list(SUBTITLE_STYLES), value=d("subtitle_style", next(iter(SUBTITLE_STYLES), None))))
                    setting("font_name", gr.Textbox(label="字幕字体（本机已安装的中文字体）", value=d("font_name", "Noto Sans CJK SC")))
                    with gr.Row():
                        setting("font_size", gr.Slider(label="字幕字号", minimum=12, maximum=160, step=1, value=d("font_size", 66)))
                        setting("subtitle_margin", gr.Slider(label="字幕距底部（像素）", minimum=10, maximum=600, step=1, value=d("subtitle_margin", 180)))
                    setting("watermark_enabled", gr.Checkbox(label="开启自定义角落水印", value=d("watermark_enabled", False)))
                    setting("watermark_text", gr.Textbox(label="水印文字", value=d("watermark_text", "")))
                    with gr.Row():
                        setting("watermark_corner", gr.Dropdown(label="水印位置", choices=["左上", "右上", "左下", "右下"], value=d("watermark_corner", "右上")))
                        setting("watermark_font_size", gr.Slider(label="水印字号", minimum=12, maximum=100, step=1, value=d("watermark_font_size", 32)))
                with gr.Column():
                    gr.Markdown("### 音频")
                    setting("bgm_volume", gr.Slider(label="BGM 音量", minimum=0, maximum=1, step=0.01, value=d("bgm_volume", 0.15)))
                    setting("voice_volume", gr.Slider(label="原片人声音量", minimum=0, maximum=2, step=0.01, value=d("voice_volume", 1.0)))
                    setting("ducking", gr.Checkbox(label="人声优先（说话时自动压低 BGM）", value=d("ducking", True)))
                    setting("denoise", gr.Checkbox(label="原片音频降噪", value=d("denoise", False)))
                    gr.Markdown("生成和导出时自动检查静音及疑似削波/爆音，并在日志与弹窗中给出时间段。检测结果需结合试听判断。")
                    with gr.Accordion("任务与片段操作", open=True):
                        selected_id = gr.Dropdown(label="选择片段 ID（可手动输入）", choices=[], allow_custom_value=True)
                        with gr.Row():
                            preview_button = gr.Button("预览所选片段")
                            regenerate_button = gr.Button("单段重新生成")
                        with gr.Row():
                            generate_button = gr.Button("只生成缺失 / 变更片段")
                            render_button = gr.Button("使用已有片段导出")
                        all_button = gr.Button("生成片段并导出成片", variant="primary")
            with gr.Row():
                clip_preview = gr.Video(label="片段预览", interactive=False, format=None)
                final_preview = gr.Video(label="成片预览", interactive=False, format=None)
            result_files = gr.File(label="下载成片 / 字幕 / 检测报告等文件", file_count="multiple", interactive=False)
            live_log = gr.Textbox(label="运行日志", lines=12, max_lines=22, interactive=False, autoscroll=True)

        with gr.Tab("3 · 项目与批量任务"):
            gr.Markdown("项目包保存剧本、素材、字幕与导出参数以及已生成片段，可移到其他目录继续编辑。API 密钥不写入项目文件。")
            with gr.Row():
                save_button = gr.Button("保存并下载项目包", variant="primary")
                export_text_button = gr.Button("导出全部台词 TXT")
            with gr.Row():
                project_download = gr.File(label="项目包下载", interactive=False)
                dialogue_download = gr.File(label="台词下载", interactive=False)
            project_upload = gr.File(label="加载项目包（.hgproj）", type="filepath", file_types=[".hgproj"])
            with gr.Accordion("恢复本机自动保存项目", open=True):
                gr.Markdown("网络中断或关闭程序后，可在这里恢复剧本、已下载片段及远程任务 ID，再继续生成。")
                autosaves = gr.Dropdown(label="本机自动保存项目", choices=[])
                with gr.Row():
                    refresh_autosaves_button = gr.Button("刷新本机项目列表")
                    recover_autosave_button = gr.Button("恢复所选自动保存")
            gr.Markdown("### 串行批量队列\n选择多个已保存项目，使用当前后端配置依次生成和导出。每个项目保留自己的字幕、配乐和导出设置。")
            batch_upload = gr.File(label="批量项目包", type="filepath", file_count="multiple", file_types=[".hgproj"])
            batch_button = gr.Button("加入串行队列并执行所有项目")

        setting_keys = list(setting_widgets)
        config_keys = list(config_widgets)
        setting_inputs = list(setting_widgets.values())
        config_inputs = list(config_widgets.values())
        # 所有完整快照操作统一从这些控件取值，避免保存项目遗漏未提交参数。
        common_inputs = [project_state, title, script_table, *setting_inputs, *config_inputs, official_key, gpu_key]
        sync_outputs = [project_state, script_table, selected_id, status]

        def collect(project: Any, project_title: str, rows: Any, *values: Any) -> tuple[Any, dict[str, Any]]:
            n = len(setting_keys)
            settings = dict(zip(setting_keys, values[:n]))
            cfg = dict(zip(config_keys, values[n:n + len(config_keys)]))
            raw = cfg.get("extra_json", "{}")
            try:
                cfg["extra_json"] = json.loads(raw or "{}") if isinstance(raw, str) else raw
            except json.JSONDecodeError as exc:
                raise ValueError(f"额外请求字段不是有效 JSON：{exc.msg}") from exc
            if not isinstance(cfg["extra_json"], dict):
                raise ValueError("额外请求字段必须是 JSON 对象，例如 {}。")
            secret_official, secret_gpu = values[n + len(config_keys):n + len(config_keys) + 2]
            settings["backend_config"] = copy.deepcopy(cfg)
            project = service.update(project, project_title, rows or [], settings)
            cfg["api_key"] = secret_official if cfg.get("backend") == "official" else secret_gpu
            return project, cfg

        def error(exc: Exception) -> gr.Error:
            return gr.Error(str(exc) or type(exc).__name__)

        def apply_rows(*args: Any) -> tuple[Any, ...]:
            try:
                project, _ = collect(*args)
                return project, rows_from_segments(project.segments), _id_update(project), "表格与参数已保存。"
            except Exception as exc:
                raise error(exc) from exc

        def import_text(text: str, duration: float, append: bool, *args: Any) -> tuple[Any, ...]:
            try:
                project, cfg = collect(*args)
                segments = parse_script_text(text or "", default_duration=float(duration))
                if not segments:
                    raise ValueError("没有可导入的台词。")
                if append and project.segments:
                    offset = max(seg.end for seg in project.segments)
                    for seg in segments:
                        seg.start += offset
                        seg.end += offset
                    all_segments = [*project.segments, *segments]
                else:
                    all_segments = segments
                project = service.update(project, project.title, rows_from_segments(all_segments), project.settings)
                return project, rows_from_segments(project.segments), _id_update(project), f"已导入 {len(segments)} 段台词。"
            except Exception as exc:
                raise error(exc) from exc

        def import_file(path: Any, duration: float, append: bool, *args: Any) -> tuple[Any, ...]:
            try:
                source = Path(_file_path(path))
                if source.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("TXT 剧本不能超过 2 MB。")
                raw = source.read_bytes()
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("gb18030")
                return import_text(text, duration, append, *args)
            except Exception as exc:
                raise error(exc) from exc

        apply_table.click(apply_rows, common_inputs, sync_outputs, **QUEUE)
        apply_bulk.click(import_text, [bulk_text, default_duration, append_mode, *common_inputs], sync_outputs, **QUEUE)
        txt_upload.upload(import_file, [txt_upload, default_duration, append_mode, *common_inputs], sync_outputs, **QUEUE)

        def ids_from_table(rows: Any) -> Any:
            ids = [str(row[0]).strip() for row in (rows or []) if row and str(row[0] or "").strip()]
            return gr.update(choices=ids)

        script_table.change(ids_from_table, script_table, selected_id, **QUEUE)

        def asset_changed(project: Any, source: Any, kind: str) -> tuple[Any, str]:
            try:
                path = _file_path(source)
                if path:
                    project = service.add_asset(project, path, kind)
                else:
                    setattr(project, kind, "")
                    project = service.update(project, project.title, rows_from_segments(project.segments), project.settings)
                return project, f"{ {'bgm': 'BGM', 'intro': '片头', 'outro': '片尾'}[kind]}已更新。"
            except Exception as exc:
                raise error(exc) from exc

        # 使用 upload/clear，避免加载项目时程序给预览赋值又重复复制素材。
        for widget, kind in [(bgm, "bgm"), (intro, "intro"), (outro, "outro")]:
            widget.upload(lambda p, source, k=kind: asset_changed(p, source, k), [project_state, widget], [project_state, status], **QUEUE)
            widget.clear(lambda p, k=kind: asset_changed(p, None, k), project_state, [project_state, status], **QUEUE)

        def save_character(project: Any, name: str, image: Any) -> tuple[Any, ...]:
            try:
                if not (name or "").strip():
                    raise ValueError("请先填写角色名称。")
                if not _file_path(image):
                    raise ValueError("请先上传角色参考图。")
                project = service.add_asset(project, _file_path(image), "characters", name.strip())
                return project, _gallery(project), f"已保存角色参考图：{name.strip()}。"
            except Exception as exc:
                raise error(exc) from exc

        def delete_character(project: Any, name: str) -> tuple[Any, ...]:
            try:
                name = (name or "").strip()
                if name not in project.characters:
                    raise ValueError("该角色尚未保存参考图。")
                project.characters.pop(name)
                project = service.update(project, project.title, rows_from_segments(project.segments), project.settings)
                return project, _gallery(project), f"已移除角色参考图：{name}。"
            except Exception as exc:
                raise error(exc) from exc

        add_character.click(save_character, [project_state, character_name, character_image], [project_state, characters_gallery, status], **QUEUE)
        remove_character.click(delete_character, [project_state, character_name], [project_state, characters_gallery, status], **QUEUE)

        def refresh_keys() -> tuple[Any, str, str]:
            names = key_store.names()
            selected = key_store.remembered_name()
            return gr.update(choices=names, value=selected), (key_store.get(selected) if selected else ""), ("已自动载入上次使用的 Key。" if selected else "")

        def save_local_key(name: str, secret: str, remember: bool) -> tuple[Any, str]:
            try:
                key_store.save(name, secret)
                if remember:
                    key_store.remember(name.strip())
                return gr.update(choices=key_store.names(), value=name.strip()), "已保存到本机。"
            except Exception as exc:
                raise error(exc) from exc

        def select_local_key(name: str | None, remember: bool) -> tuple[str, str]:
            try:
                secret = key_store.get(name)
                if remember:
                    key_store.remember(name)
                return secret, "已载入所选 Key，可用于生成。"
            except Exception as exc:
                raise error(exc) from exc

        def delete_local_key(name: str | None) -> tuple[Any, str, str]:
            try:
                key_store.delete(name)
                choices, _, _ = refresh_keys()
                return choices, "", "已删除所选本地 Key。"
            except Exception as exc:
                raise error(exc) from exc

        save_key_button.click(save_local_key, [key_name, official_key, remember_key], [saved_key, key_notice], **QUEUE)
        select_key_button.click(select_local_key, [saved_key, remember_key], [official_key, key_notice], **QUEUE)
        delete_key_button.click(delete_local_key, saved_key, [saved_key, official_key, key_notice], **QUEUE)
        clear_key_button.click(lambda: ("", "输入框已清空；已保存的 Key 不受影响。"), None, [official_key, key_notice], **QUEUE)

        def change_remember(enabled: bool, selected: str | None) -> str:
            if not enabled:
                key_store.forget_selection()
                return "已关闭自动记住。"
            if selected:
                key_store.remember(selected)
                return "已记住当前选择，下次打开网页会自动载入。"
            return "请选择并使用一个已保存的 Key。"

        remember_key.change(change_remember, [remember_key, saved_key], key_notice, **QUEUE)

        def switch_backend(value: str) -> tuple[Any, ...]:
            official = value == "official"
            return (
                gr.update(visible=official), gr.update(visible=not official),
                "https://api.minimax.cn" if official else "http://127.0.0.1:8000",
                "/v2/video_generation" if official else "/generate",
                "/v2/query/video_generation/{task_id}" if official else "/tasks/{task_id}",
            )

        backend.input(switch_backend, backend, [official_key, gpu_key, base_url, create_path, query_path], **QUEUE)

        def use_preset(name: str) -> list[Any]:
            values = EXPORT_PRESETS.get(name, {})
            return [values.get(key, d(key, None)) for key in ["width", "height", "fps", "video_bitrate", "audio_bitrate", "sample_rate"]]

        preset.change(use_preset, preset, [setting_widgets[key] for key in ["width", "height", "fps", "video_bitrate", "audio_bitrate", "sample_rate"]], **QUEUE)

        job_outputs = [project_state, script_table, selected_id, clip_preview, final_preview, result_files, live_log, status]

        def make_job(mode: str):
            def execute(selected: str | None, *args: Any):
                log_text = ""
                try:
                    project, cfg = collect(*args)
                    if mode == "regenerate" and not selected:
                        raise ValueError("请选择需要重新生成的片段 ID。")
                    yield project, rows_from_segments(project.segments), _id_update(project, selected), gr.skip(), gr.skip(), gr.skip(), "开始执行…", "任务执行中。"
                    seen: set[str] = set()
                    # generator 的最后一次 yield 使用明确文件路径，不使用 gr.skip()。
                    # Gradio 5 在某些浏览器中会把“跳过”解析成清空 Video 的更新；
                    # 显式保留最后一个片段与成片路径可保证任务完成后仍显示内嵌播放器。
                    last_clip: str | None = None
                    last_output: str | None = None
                    last_files: list[str] | None = None
                    for event in service.run(project, cfg, mode=mode, selected_id=selected or ""):
                        project = event.get("project", project)
                        log_text = event.get("log", log_text)
                        if event.get("clip"):
                            last_clip = str(event["clip"])
                        if event.get("output"):
                            last_output = str(event["output"])
                        if event.get("files"):
                            last_files = [str(item) for item in event["files"]]
                        fresh = [str(item) for item in event.get("warnings", []) if str(item) not in seen]
                        if fresh:
                            seen.update(fresh)
                            gr.Warning("\n".join(fresh[:8]), duration=12)
                        yield (
                            project, rows_from_segments(project.segments), _id_update(project, selected),
                            last_clip or gr.skip(), last_output or gr.skip(),
                            last_files or gr.skip(), log_text, "任务执行中，请查看实时日志。",
                        )
                    # Never emit skip for the video outputs after a successful job.
                    yield project, rows_from_segments(project.segments), _id_update(project, selected), last_clip, last_output, last_files or [], log_text, "任务完成。"
                except Exception as exc:
                    gr.Warning(f"任务未完成：{exc}", duration=12)
                    raise error(exc) from exc
            return execute

        for button, mode in [(all_button, "all"), (generate_button, "generate"), (render_button, "render"), (regenerate_button, "regenerate")]:
            button.click(make_job(mode), [selected_id, *common_inputs], job_outputs, **QUEUE)

        def preview(selected: str | None, *args: Any) -> tuple[Any, ...]:
            try:
                project, _ = collect(*args)
                segment = next((seg for seg in project.segments if seg.id == selected), None)
                if segment is None or not _existing(segment.clip_path):
                    raise ValueError("该片段还没有可预览的视频，请先生成。")
                return project, segment.clip_path, f"正在预览片段：{selected}。"
            except Exception as exc:
                raise error(exc) from exc

        preview_button.click(preview, [selected_id, *common_inputs], [project_state, clip_preview, status], **QUEUE)

        def save_project(*args: Any) -> tuple[Any, ...]:
            try:
                project, _ = collect(*args)
                destination = service.save(project)
                return project, rows_from_segments(project.segments), _id_update(project), destination, "项目包已保存，可点击文件下载。"
            except Exception as exc:
                raise error(exc) from exc

        save_button.click(save_project, common_inputs, [project_state, script_table, selected_id, project_download, status], **QUEUE)

        def export_dialogue(*args: Any) -> tuple[Any, ...]:
            try:
                project, _ = collect(*args)
                return project, service.export_dialogue(project), "全部台词已导出。"
            except Exception as exc:
                raise error(exc) from exc

        export_text_button.click(export_dialogue, common_inputs, [project_state, dialogue_download, status], **QUEUE)

        hydrate_outputs = [project_state, title, script_table, selected_id, characters_gallery, bgm, intro, outro, *setting_inputs, *config_inputs, official_key, gpu_key, clip_preview, final_preview, result_files, live_log, status]

        def hydrate(project: Any, message: str) -> list[Any]:
            saved = {**defaults, **project.settings}
            cfg = {"backend": "official", "base_url": "https://api.minimax.cn", "model": "MiniMax-H3", "resolution": "768P", "ratio": "9:16", "create_path": "/v2/video_generation", "query_path": "/v2/query/video_generation/{task_id}", "timeout": 60, "poll_interval": 5, "max_wait": 1800, "extra_json": {}, **saved.get("backend_config", {})}
            if not isinstance(cfg.get("extra_json"), str):
                cfg["extra_json"] = json.dumps(cfg.get("extra_json") or {}, ensure_ascii=False, indent=2)
            return [
                project, project.title, rows_from_segments(project.segments), _id_update(project), _gallery(project),
                _existing(project.bgm), _existing(project.intro), _existing(project.outro),
                *[saved.get(key) for key in setting_keys], *[cfg.get(key) for key in config_keys],
                gr.update(value="", visible=cfg["backend"] == "official"),
                gr.update(value="", visible=cfg["backend"] == "gpu"),
                _first_clip(project), _existing(saved.get("last_output")), [], "", message,
            ]

        def load_project(source: Any) -> list[Any]:
            try:
                project = service.load(_file_path(source))
                return hydrate(project, f"已加载项目：{project.title}。")
            except Exception as exc:
                raise error(exc) from exc

        def new_project() -> list[Any]:
            try:
                return hydrate(service.new_project(), "已新建项目。原项目素材仍保留在数据目录中。")
            except Exception as exc:
                raise error(exc) from exc

        def refresh_autosaves() -> Any:
            try:
                choices = service.list_autosaves()
                return gr.update(choices=choices, value=choices[0][1] if choices else None)
            except Exception as exc:
                raise error(exc) from exc

        def recover_autosave(project_id: str | None) -> list[Any]:
            try:
                if not project_id:
                    raise ValueError("请先选择一个本机自动保存项目。")
                project = service.load_autosave(project_id)
                return hydrate(project, f"已恢复自动保存：{project.title}。可继续查询已保存的远程任务。")
            except Exception as exc:
                raise error(exc) from exc

        refresh_autosaves_button.click(refresh_autosaves, None, autosaves, **QUEUE)
        recover_autosave_button.click(recover_autosave, autosaves, hydrate_outputs, **QUEUE)
        project_upload.upload(load_project, project_upload, hydrate_outputs, **QUEUE)
        new_button.click(new_project, None, hydrate_outputs, **QUEUE)
        demo.load(new_project, None, hydrate_outputs, **QUEUE).then(refresh_autosaves, None, autosaves, **QUEUE).then(refresh_keys, None, [saved_key, official_key, key_notice], **QUEUE)

        def batch_projects(paths: list[Any] | None, *args: Any):
            try:
                _, cfg = collect(*args)
                if not paths:
                    raise ValueError("请先选择需要批量处理的项目包。")
                current = args[0]
                seen: set[str] = set()
                all_files: list[str] = []
                last_clip: str | None = None
                last_output: str | None = None
                last_log = ""
                for event in service.batch([_file_path(path) for path in paths], cfg):
                    current = event.get("project", current)
                    if event.get("clip"):
                        last_clip = str(event["clip"])
                    if event.get("output"):
                        last_output = str(event["output"])
                    for item in event.get("files", []):
                        if item not in all_files:
                            all_files.append(item)
                    last_log = event.get("log", last_log)
                    fresh = [str(item) for item in event.get("warnings", []) if str(item) not in seen]
                    if fresh:
                        seen.update(fresh)
                        gr.Warning("\n".join(fresh[:8]), duration=12)
                    yield last_clip or gr.skip(), last_output or gr.skip(), all_files or gr.skip(), last_log, "串行批量任务执行中。"
                yield last_clip, last_output, all_files, last_log, "全部批量任务已结束；请查看日志及下载文件。"
            except Exception as exc:
                raise error(exc) from exc

        batch_button.click(batch_projects, [batch_upload, *common_inputs], [clip_preview, final_preview, result_files, live_log, status], **QUEUE)

    return demo.queue(default_concurrency_limit=1, max_size=64)
