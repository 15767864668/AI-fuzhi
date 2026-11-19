"""Local, single-user MiniMax API key profiles.

Keys are stored outside project exports. This is intentionally a local-only
plaintext store; protect the computer and never expose the Gradio server publicly.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class LocalKeyStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise ValueError("本地 API Key 文件格式不正确。")
        return data

    def names(self) -> list[str]:
        return sorted(self._read())

    def get(self, name: str | None) -> str:
        if not name:
            raise ValueError("请先选择已保存的 Key。")
        data = self._read()
        if name not in data:
            raise ValueError("找不到该 Key，请刷新列表。")
        return data[name]

    def save(self, name: str, secret: str) -> None:
        name = (name or "").strip()
        secret = (secret or "").strip()
        if not name or not secret:
            raise ValueError("请填写密钥名称和 API Key。")
        if len(name) > 80 or len(secret) > 4096:
            raise ValueError("名称或 API Key 长度超出限制。")
        data = self._read()
        data[name] = secret
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        finally:
            if temp.exists():
                temp.unlink()

    def delete(self, name: str | None) -> None:
        if not name:
            raise ValueError("请先选择要删除的 Key。")
        data = self._read()
        if name not in data:
            raise ValueError("该 Key 已不存在。")
        del data[name]
        if self.remembered_name() == name:
            self.forget_selection()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        finally:
            if temp.exists():
                temp.unlink()

    @property
    def selection_path(self) -> Path:
        return self.path.with_name("api_key_selection.json")

    def remembered_name(self) -> str | None:
        if not self.selection_path.exists():
            return None
        try:
            value = json.loads(self.selection_path.read_text(encoding="utf-8"))
            name = value.get("name") if isinstance(value, dict) else None
            return name if isinstance(name, str) and name in self._read() else None
        except (ValueError, OSError):
            return None

    def remember(self, name: str | None) -> None:
        if not name or name not in self._read():
            raise ValueError("请选择一个已保存的 Key。")
        self.selection_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.selection_path.with_name(self.selection_path.name + ".tmp")
        try:
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({"name": name}, output, ensure_ascii=False)
            os.replace(temp, self.selection_path)
            if os.name != "nt":
                os.chmod(self.selection_path, 0o600)
        finally:
            if temp.exists():
                temp.unlink()

    def forget_selection(self) -> None:
        self.selection_path.unlink(missing_ok=True)
