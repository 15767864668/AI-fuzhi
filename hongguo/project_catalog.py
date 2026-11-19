"""Local project catalog for the Studio V2 workspace.

The catalog operates on the existing autosave format, never on API key files.
All destructive operations are explicitly opt-in.
"""
from __future__ import annotations

import copy
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Project
from .service import StudioService


class ProjectCatalog:
    def __init__(self, workspace: Path):
        self.service = StudioService(workspace)
        self.root = self.service.storage.root

    def list_projects(self) -> list[dict]:
        results = []
        for label, project_id in self.service.list_autosaves():
            try:
                project = self.service.load_autosave(project_id)
                path = self.root / project_id / "projects" / "autosave.json"
                results.append({
                    "id": project.id,
                    "title": project.title,
                    "segment_count": len(project.segments),
                    "updated_at": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat(),
                })
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return results

    def open(self, project_id: str) -> Project:
        return self.service.load_autosave(project_id)

    def save(self, project: Project) -> None:
        self.service.storage.checkpoint(project)

    def rename(self, project_id: str, title: str) -> Project:
        title = title.strip()
        if not title or len(title) > 100:
            raise ValueError("项目名称不能为空，且不能超过100个字符")
        project = self.open(project_id)
        project.title = title
        self.save(project)
        return project

    def duplicate(self, project_id: str) -> Project:
        original = self.open(project_id)
        cloned = copy.deepcopy(original)
        cloned.id = uuid.uuid4().hex[:12]
        cloned.title = original.title + "（副本）"
        cloned.created_at = datetime.now(timezone.utc).isoformat()
        source_root = (self.root / original.id).resolve()
        dest_root = self.root / cloned.id

        def copy_reference(reference: str, category: str) -> str:
            if not reference:
                return ""
            source = Path(reference).resolve()
            if not source.is_file() or not source.is_relative_to(source_root):
                raise ValueError("项目素材缺失或位于项目目录之外")
            return self.service.storage.copy_asset(cloned, str(source), category)

        try:
            cloned.characters = {
                name: copy_reference(path, "characters")
                for name, path in original.characters.items()
            }
            for field, category in (("bgm", "bgm"), ("intro", "media"), ("outro", "media")):
                setattr(cloned, field, copy_reference(getattr(original, field), category))
            for segment in cloned.segments:
                segment.clip_path = copy_reference(segment.clip_path, "clips")
                segment.remote_task = {}
            cloned.settings.pop("last_output", None)
            self.save(cloned)
            return cloned
        except Exception:
            shutil.rmtree(dest_root, ignore_errors=True)
            raise

    def delete(self, project_id: str, *, confirm: bool = False) -> None:
        if not confirm:
            raise ValueError("删除项目需要明确确认")
        project = self.open(project_id)
        target = (self.root / project.id).resolve()
        if target.parent != self.root or target.is_symlink():
            raise ValueError("项目目录不安全")
        shutil.rmtree(target)
