"""Offline API tests: no H3 calls or GPU required."""
from fastapi.testclient import TestClient

from api import app as api_module
from hongguo.project_catalog import ProjectCatalog


def test_project_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "catalog", ProjectCatalog(tmp_path))
    client = TestClient(api_module.app)
    assert client.get("/api/health").json()["status"] == "ok"
    response = client.post("/api/projects", json={"title": "第一集"})
    assert response.status_code == 201
    project_id = response.json()["id"]
    assert len(client.get("/api/projects").json()) == 1
    assert client.get(f"/api/projects/{project_id}").json()["title"] == "第一集"
    assert client.patch(f"/api/projects/{project_id}", json={"title": "改名"}).json()["title"] == "改名"
    cloned = client.post(f"/api/projects/{project_id}/duplicate")
    assert cloned.status_code == 201
    assert cloned.json()["id"] != project_id
    assert client.post(f"/api/projects/{project_id}/delete", json={"confirm": False}).status_code == 400
    assert client.post(f"/api/projects/{project_id}/delete", json={"confirm": True}).json()["deleted"]
    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert len(client.get("/api/projects").json()) == 1
