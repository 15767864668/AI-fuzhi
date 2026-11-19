"""界面与核心流程的边界。串行任务、恢复缓存、日志均在这里协调。"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import queue
import threading
from typing import Iterator
import uuid

from filelock import FileLock
from .backends import generate_clip, OFFICIAL_BASE, OFFICIAL_CREATE
from .models import Project, Segment, validate_segments, validate_settings
from .script import segments_from_rows
from .storage import Storage, scrub_secrets, safe_name


class StudioService:
    def __init__(self, workspace: Path):
        self.storage = Storage(workspace)
        # Gradio 共享并发组排队；文件锁额外保护多个本地进程。
        self.task_lock = FileLock(str(self.storage.root / ".studio.lock"), timeout=-1)

    def new_project(self, title: str = "未命名短剧") -> Project:
        project = Project(title=title.strip() or "未命名短剧")
        self.storage.checkpoint(project)
        return project

    def update(self, project: Project, title: str, rows, settings: dict) -> Project:
        updated = copy.deepcopy(project)
        updated.title = title.strip() or "未命名短剧"
        # 空表格可以保存草稿，开始任务时另行验证。
        effective_rows = rows.values.tolist() if hasattr(rows, "values") else rows
        has_values = any(any(str(v).strip() for v in row if v is not None) for row in (effective_rows or []))
        updated.segments = segments_from_rows(effective_rows, project.segments) if has_values else []
        updated.settings.update(scrub_secrets(settings))
        validate_settings(updated.settings)
        self.storage.checkpoint(updated)
        return updated

    def add_asset(self, project: Project, path: str | None, kind: str, character: str = "") -> Project:
        updated = copy.deepcopy(project)
        if kind == "characters":
            character = character.strip()
            if not character:
                raise ValueError("请先填写角色名")
            if path:
                from PIL import Image
                with Image.open(path) as image:
                    image.verify()
                updated.characters[character] = self.storage.copy_asset(updated, path, "characters")
            else:
                updated.characters.pop(character, None)
        elif kind in ("bgm", "intro", "outro"):
            if path:
                from .media import probe
                info = probe(path)
                expected = "audio" if kind == "bgm" else "video"
                if not any(stream.get("codec_type") == expected for stream in info.get("streams", [])):
                    raise ValueError("素材不包含所需的音轨或视频轨")
            setattr(updated, kind, self.storage.copy_asset(updated, path, "bgm" if kind == "bgm" else "media") if path else "")
        else:
            raise ValueError("未知素材类型")
        self.storage.checkpoint(updated)
        return updated

    def save(self, project: Project) -> str:
        return self.storage.export_bundle(project)

    def load(self, path: str) -> Project:
        return self.storage.load_bundle(path)

    def list_autosaves(self) -> list[tuple[str, str]]:
        projects = []
        for path in sorted(self.storage.root.glob("*/projects/autosave.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                projects.append((f"{data.get('title', '未命名')} · {path.parent.parent.name}", path.parent.parent.name))
            except (OSError, ValueError):
                continue
        return projects

    def load_autosave(self, project_id: str) -> Project:
        """读取本机受管理的自动保存，恢复尚未下载的远程任务。"""
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(project_id)):
            raise ValueError("请选择有效的本机项目")
        base = self.storage.root / project_id
        path = base / "projects" / "autosave.json"
        project = Project.from_dict(scrub_secrets(json.loads(path.read_text(encoding="utf-8"))))
        if project.id != project_id:
            raise ValueError("项目 ID 与自动保存目录不一致")
        referenced = list(project.characters.values()) + [project.bgm, project.intro, project.outro] + [s.clip_path for s in project.segments]
        for reference in referenced:
            if reference and not Path(reference).resolve().is_relative_to(base.resolve()):
                raise ValueError("自动保存中的素材超出了项目目录，请改用项目包导入")
        return project

    def export_dialogue(self, project: Project) -> str:
        path = self.storage.folder(project, "projects") / f"{safe_name(project.title)}_全部台词.txt"
        path.write_text("\n".join((f"{s.character}：" if s.character else "") + s.text for s in project.segments) + "\n", encoding="utf-8")
        return str(path)

    def references(self, project: Project, segment: Segment) -> list[str]:
        if segment.character:
            # 单段可用中英文逗号引用多个角色；一个名称对应一张参考图。
            names = [name.strip() for name in segment.character.replace("，", ",").split(",") if name.strip()]
            missing = [name for name in names if name not in project.characters]
            if missing:
                # 无参考图也允许角色名：只通过 prompt 描述身份；有图时缺失名称需明确提示。
                if project.characters:
                    raise ValueError(f"角色参考图未配置：{'、'.join(missing)}")
                return []
            return [project.characters[name] for name in names]
        return list(project.characters.values())

    def fingerprint(self, project: Project, segment: Segment, config: dict) -> str:
        references = []
        for path in self.references(project, segment):
            digest = hashlib.sha256()
            with Path(path).open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            references.append(digest.hexdigest())
        backend = config.get("backend", "official")
        relevant = {
            "backend": backend,
            "base_url": str(config.get("base_url") or (OFFICIAL_BASE if backend == "official" else "http://127.0.0.1:8000")).rstrip("/"),
            "model": config.get("model") or "MiniMax-H3",
            "resolution": config.get("resolution") or "768P", "ratio": config.get("ratio") or "9:16",
            "create_path": config.get("create_path") or (OFFICIAL_CREATE if backend == "official" else "/generate"),
            "extra_json": config.get("extra_json") or {},
        }
        if isinstance(relevant["extra_json"], str):
            relevant["extra_json"] = json.loads(relevant["extra_json"])
        payload = {"text": segment.text, "prompt": segment.prompt, "character": segment.character,
                   "duration": round(segment.duration, 6), "trim_start": segment.trim_start,
                   "references": references, "config": relevant}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def run(self, project: Project, config: dict, mode: str = "all", selected_id: str = "") -> Iterator[dict]:
        """后台线程执行阻塞 HTTP/FFmpeg，生成器立即推送实时日志。"""
        if mode not in {"all", "generate", "render", "regenerate"}:
            raise ValueError("未知任务模式")
        project = copy.deepcopy(project)
        validate_segments(project.segments)
        validate_settings(project.settings)
        events: queue.Queue = queue.Queue()
        logs: list[str] = []
        warnings: list[str] = []
        state = {"project": project, "clip": None, "output": None, "files": [], "warnings": warnings}
        log_path = self.storage.folder(project, "logs") / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.log"

        def log(message: str):
            # 不输出请求体、API Key 或带签名的视频 URL。供应商报错也再次脱敏。
            message = str(message)
            secret_values = [config.get("api_key", "")]
            import os
            secret_values += [os.getenv("MINIMAX_API_KEY", ""), os.getenv("H3_GPU_API_KEY", "")]
            for secret in secret_values:
                if secret:
                    message = message.replace(secret, "[已隐藏]")
            stamp = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
            logs.append(stamp)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(stamp + "\n")
            # UI 最多保留 1000 行，完整内容写入文件。
            events.put({"project": copy.deepcopy(project), **{k: copy.deepcopy(v) for k, v in state.items() if k != "project"}, "log": "\n".join(logs[-1000:])})

        def worker():
            try:
                log("任务已排队，等待本机处理锁")
                with self.task_lock:
                    from .media import render_project, probe, analyze_audio
                    log(f"开始处理：{project.title}（模式：{mode}）")
                    project.settings["backend_config"] = scrub_secrets(config)
                    targets = project.segments
                    if mode == "regenerate":
                        targets = [s for s in project.segments if s.id == selected_id]
                        if not targets:
                            raise ValueError("请选择有效片段 ID")
                        log("单段重新生成：将创建新的生成任务")
                    if mode != "render":
                        for index, segment in enumerate(targets, 1):
                            expected = self.fingerprint(project, segment, config)
                            if mode != "regenerate" and segment.fingerprint == expected and segment.clip_path and Path(segment.clip_path).is_file():
                                state["clip"] = segment.clip_path
                                log(f"[{index}/{len(targets)}] {segment.id} 复用已生成片段")
                                continue
                            if mode == "regenerate" or segment.fingerprint != expected:
                                segment.remote_task = {}
                                segment.clip_path = ""
                            segment.fingerprint = expected
                            self.storage.checkpoint(project)
                            destination = self.storage.folder(project, "clips") / f"{segment.id}_{uuid.uuid4().hex[:8]}.mp4"

                            def checkpoint(task: dict):
                                segment.remote_task = scrub_secrets(task)
                                self.storage.checkpoint(project)

                            log(f"[{index}/{len(targets)}] 生成片段 {segment.id}，台词时长 {segment.duration:.2f} 秒")
                            result = generate_clip(segment, self.references(project, segment), config, destination, log, checkpoint)
                            actual_path = result["local_path"]
                            info = probe(actual_path)
                            streams = info.get("streams", [])
                            if not any(s.get("codec_type") == "video" for s in streams) or not any(s.get("codec_type") == "audio" for s in streams):
                                raise RuntimeError("H3 结果缺少视频或原生人声音轨，请检查后端输出")
                            segment.clip_path = actual_path
                            state["clip"] = actual_path
                            self.storage.checkpoint(project)
                            found = analyze_audio(actual_path, log)
                            warnings.extend(found)
                            for warning in found:
                                log("音频提醒：" + warning)
                            log(f"片段 {segment.id} 下载并检查完成")
                    if mode in {"all", "render"}:
                        for segment in project.segments:
                            if not segment.clip_path or not Path(segment.clip_path).is_file():
                                raise ValueError(f"片段 {segment.id} 尚未生成，请先生成片段")
                            if segment.fingerprint != self.fingerprint(project, segment, config):
                                raise ValueError(f"片段 {segment.id} 的台词、时长、参考图或后端参数已变更，请先生成更新片段")
                        output = self.storage.next_output(project)
                        try:
                            result = render_project(project, output, log)
                        except Exception:
                            output.unlink(missing_ok=True)
                            raise
                        state["output"] = result["output"]
                        project.settings["last_output"] = result["output"]
                        warnings.extend(result.get("warnings", []))
                        state["files"] = [result["output"]]
                        subtitle = result.get("subtitle")
                        if subtitle and Path(subtitle).is_file():
                            import shutil
                            classified = self.storage.folder(project, "subtitles") / Path(subtitle).name
                            shutil.move(subtitle, classified)
                            state["files"].append(str(classified))
                        report = output.with_suffix(".report.json")
                        report.write_text(json.dumps({"project": project.title, "output": output.name,
                            "warnings": list(dict.fromkeys(warnings)), "media": probe(result["output"]),
                            "note": "本地媒体与音频预检；红果最终上传规则以平台后台为准。"}, ensure_ascii=False, indent=2), encoding="utf-8")
                        state["files"].append(str(report))
                    self.storage.checkpoint(project)
                    state["files"] += [self.export_dialogue(project), str(log_path), str(self.storage.checkpoint(project))]
                    log("任务完成；缓存、台词、日志和项目自动保存已写入分类目录")
            except Exception as exc:
                log(f"任务失败：{exc}")
                self.storage.checkpoint(project)
                events.put(exc)
            finally:
                events.put(None)

        threading.Thread(target=worker, name=f"studio-{project.id}", daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                break
            if isinstance(event, Exception):
                raise event
            yield event

    def batch(self, paths: list[str], config: dict) -> Iterator[dict]:
        """一个项目失败不阻塞后续项目；每个项目依次完成生成及导出。"""
        if not paths:
            raise ValueError("请先选择要排队的 .hgproj 项目")
        history = []
        completed_files: list[str] = []
        for index, path in enumerate(paths, 1):
            project = Project(title=Path(path).stem)
            prefix = f"批量任务 {index}/{len(paths)}：{project.title}"
            try:
                project = self.load(path)
                prefix = f"批量任务 {index}/{len(paths)}：{project.title}"
                for event in self.run(project, config, "all"):
                    event["log"] = "\n".join(history + [prefix, event["log"]])
                    event["files"] = list(dict.fromkeys(completed_files + event.get("files", [])))
                    yield event
                completed_files = event.get("files", [])
                history.append(prefix + " 完成")
            except Exception as exc:
                history.append(prefix + f" 失败：{exc}")
                yield {"project": project, "log": "\n".join(history), "clip": None, "output": None,
                       "files": completed_files, "warnings": ["该项目失败，队列将继续下一个项目"]}
