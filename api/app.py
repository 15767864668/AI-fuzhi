"""Local-only Studio V2 HTTP API.

Run with: uvicorn api.app:app --host 127.0.0.1 --port 8765
Do not expose this unauthenticated development API to a public network.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from hongguo.project_catalog import ProjectCatalog

app = FastAPI(title="H3 Short Drama Studio V2", version="0.1.0")
workspace = Path(os.getenv("HONGGUO_DATA_DIR", "./data")).expanduser().resolve()
catalog = ProjectCatalog(workspace)


class ProjectInput(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ProjectPatch(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class DeleteConfirmation(BaseModel):
    confirm: bool = False


def public_project(project: Any) -> dict:
    # Never return local filesystem paths, API credentials, or remote task tokens.
    return {
        "id": project.id,
        "title": project.title,
        "created_at": project.created_at,
        "segment_count": len(project.segments),
        "characters": sorted(project.characters.keys()),
    }


def project_or_404(project_id: str):
    try:
        return catalog.open(project_id)
    except (ValueError, OSError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="找不到项目") from exc


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/projects")
def list_projects() -> list[dict]:
    return catalog.list_projects()


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectInput) -> dict:
    return public_project(catalog.service.new_project(body.title))


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    return public_project(project_or_404(project_id))


@app.patch("/api/projects/{project_id}")
def rename_project(project_id: str, body: ProjectPatch) -> dict:
    project_or_404(project_id)
    try:
        return public_project(catalog.rename(project_id, body.title))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/duplicate", status_code=201)
def duplicate_project(project_id: str) -> dict:
    project_or_404(project_id)
    try:
        return public_project(catalog.duplicate(project_id))
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/delete")
def delete_project(project_id: str, body: DeleteConfirmation) -> dict:
    project_or_404(project_id)
    if not body.confirm:
        raise HTTPException(status_code=400, detail="需要确认删除")
    catalog.delete(project_id, confirm=True)
    return {"deleted": True, "id": project_id}
