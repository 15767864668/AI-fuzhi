"""项目目录、原子保存和可搬移项目包。项目包不包含 API 密钥。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid
import zipfile
from datetime import datetime

from .models import Project

SENSITIVE = {"api_key", "apikey", "authorization", "token", "access_token", "secret", "password", "_transport"}


def scrub_secrets(value):
    if isinstance(value, dict):
        return {k: scrub_secrets(v) for k, v in value.items() if str(k).lower() not in SENSITIVE and not str(k).startswith("_")}
    if isinstance(value, list):
        return [scrub_secrets(v) for v in value]
    return value


def safe_name(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:80]
    if not name or name.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        return "未命名短剧"
    return name


class Storage:
    CATEGORIES = ("characters", "clips", "subtitles", "bgm", "exports", "projects", "media", "logs")

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def folder(self, project: Project, category: str = "") -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project.id):
            raise ValueError("无效项目 ID")
        if category and category not in self.CATEGORIES:
            raise ValueError("未知素材分类")
        base = self.root / project.id
        for name in self.CATEGORIES:
            (base / name).mkdir(parents=True, exist_ok=True)
        return base / category

    def copy_asset(self, project: Project, source: str, category: str) -> str:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise ValueError("上传素材不存在")
        digest = hashlib.sha256()
        with source_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        extension = source_path.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
            extension = ".bin"
        target = self.folder(project, category) / (digest.hexdigest()[:24] + extension)
        if target != source_path and not target.exists():
            temporary = target.with_suffix(extension + ".tmp")
            shutil.copyfile(source_path, temporary)
            temporary.replace(target)
        return str(target)

    def checkpoint(self, project: Project) -> Path:
        destination = self.folder(project, "projects") / "autosave.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(scrub_secrets(project.to_dict()), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
        return destination

    def next_output(self, project: Project, extension: str = ".mp4") -> Path:
        """调用方持有全局任务锁；额外用 x 模式防止覆盖已有文件。"""
        directory = self.folder(project, "exports")
        prefix = f"{safe_name(project.title)}_{datetime.now().strftime('%Y%m%d')}_"
        for sequence in range(1, 1000000):
            path = directory / f"{prefix}{sequence:03}{extension}"
            try:
                with path.open("xb"):
                    pass
                return path
            except FileExistsError:
                continue
        raise ValueError("当日输出序号已用尽")

    def export_bundle(self, project: Project) -> str:
        """ZIP 包只收录明确引用的素材、缓存片段和项目 JSON。"""
        payload = copy.deepcopy(project)
        payload.settings.pop("last_output", None)  # 成片另行导出，项目包不保留失效的机器绝对路径。
        entries: dict[str, Path] = {}

        def include(path: str, category: str) -> str:
            if not path:
                return ""
            source = Path(path)
            if not source.is_file():
                raise ValueError(f"项目引用的素材缺失：{source.name}")
            # 文件名来自本客户端管理目录，仍附加摘要防止名称冲突。
            name = f"{category}/{hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:12]}_{safe_name(source.name)}"
            entries[name] = source
            return name

        payload.characters = {name: include(path, "characters") for name, path in payload.characters.items()}
        for field, category in (("bgm", "bgm"), ("intro", "media"), ("outro", "media")):
            setattr(payload, field, include(getattr(payload, field), category))
        for segment in payload.segments:
            segment.clip_path = include(segment.clip_path, "clips")
        document = {"format": "hongguo-studio", "version": 1, "project": scrub_secrets(payload.to_dict())}
        target = self.folder(project, "projects") / f"{safe_name(project.title)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.hgproj"
        temporary = target.with_suffix(".tmp")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                archive.writestr("project.json", json.dumps(document, ensure_ascii=False, indent=2))
                for name, source in entries.items():
                    archive.write(source, name)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        self.checkpoint(project)
        return str(target)

    def load_bundle(self, source: str) -> Project:
        """限制解压边界，拒绝路径穿越、符号链接和异常膨胀的项目。"""
        with zipfile.ZipFile(source) as archive:
            items = archive.infolist()
            if len(items) > 20000 or sum(info.file_size for info in items) > 20 * 1024 ** 3:
                raise ValueError("项目超过 20GB 或 20000 个文件限制")
            names: set[str] = set()
            for info in items:
                name = PurePosixPath(info.filename)
                if (info.filename in names or "\\" in info.filename or name.is_absolute() or ".." in name.parts
                        or ":" in info.filename or stat.S_ISLNK(info.external_attr >> 16)):
                    raise ValueError("项目包包含不安全或重复路径")
                if info.filename != "project.json" and (not name.parts or name.parts[0] not in {"characters", "clips", "bgm", "media"}):
                    raise ValueError("项目包含未知文件分类")
                names.add(info.filename)
            info = archive.getinfo("project.json")
            if info.file_size > 10 * 1024 * 1024:
                raise ValueError("项目描述文件过大")
            document = json.loads(archive.read(info).decode("utf-8"))
            if document.get("format") != "hongguo-studio" or document.get("version") != 1:
                raise ValueError("不支持的项目格式或版本")
            project = Project.from_dict(scrub_secrets(document["project"]))
            project.id = uuid.uuid4().hex[:12]  # 加载为独立副本，绝不覆盖当前项目。
            base = self.folder(project)
            try:
                for info in items:
                    if info.filename == "project.json" or info.is_dir():
                        continue
                    target = base.joinpath(*PurePosixPath(info.filename).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)

                def resolve(path: str) -> str:
                    if not path:
                        return ""
                    if path not in names or PurePosixPath(path).is_absolute():
                        raise ValueError("项目素材路径未包含在项目包中")
                    return str(base.joinpath(*PurePosixPath(path).parts))

                project.characters = {name: resolve(path) for name, path in project.characters.items()}
                for field in ("bgm", "intro", "outro"):
                    setattr(project, field, resolve(getattr(project, field)))
                for segment in project.segments:
                    segment.clip_path = resolve(segment.clip_path)
                self.checkpoint(project)
                return project
            except Exception:
                shutil.rmtree(base, ignore_errors=True)
                raise
